"""Provider-free creator direction behavior against migrated PostgreSQL."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.database import AsyncSessionLocal
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorMemoryItem,
    CreatorMemoryOperation,
    CreatorMemoryOutbox,
    ProjectDirectionOverride,
    User,
)
from app.services.creator_direction import (
    CreatorDirectionResolver,
    CreatorDirectionService,
    DirectionConflict,
    DirectionError,
    IdempotencyMismatch,
    LimitReached,
    StaleRevision,
)
from app.services.creator_direction_snapshot import (
    apply_direction_overrides,
    attach_snapshot,
    typed_overrides_from_container,
)


async def _create_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=user_id, email=f"creator-memory-{user_id}@example.test"))
        await db.commit()
    return user_id


async def _delete_users(*user_ids: uuid.UUID) -> None:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(User).where(User.id.in_(user_ids)))
            await db.commit()
    except (OperationalError, OSError):
        pass


@pytest.mark.asyncio(loop_scope="module")
async def test_activate_resolve_replay_undo_and_soft_suggestion():
    user_id = uuid.uuid4()
    service = CreatorDirectionService()
    try:
        async with AsyncSessionLocal() as db:
            db.add(User(id=user_id, email=f"creator-memory-{user_id}@example.test"))
            await db.commit()

            created = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows to my videos",
                category="other",
                enforcement="default",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="smoke-create",
                source_kind="creation_thread",
                user_locked=False,
            )
            replay = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows to my videos",
                category="other",
                enforcement="default",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="smoke-create",
                source_kind="creation_thread",
                user_locked=False,
            )
            assert replay.id == created.id

            snapshot = await CreatorDirectionResolver().snapshot(db, user_id)
            assert snapshot.revision == 1
            assert snapshot.typed_overrides == {"shadow_enabled": False}

            await service.undo(
                db,
                user_id,
                created.id,
                expected_revision=1,
                idempotency_key="smoke-undo",
            )
            await service.create_item(
                db,
                user_id,
                instruction="I prefer quick cuts",
                category="stories_pacing",
                enforcement="advisory",
                normalized_key=None,
                structured_value=None,
                expected_revision=None,
                idempotency_key="smoke-suggestion",
                source_kind="creation_thread",
                user_locked=False,
                initial_state="suggested",
            )
            await db.commit()

            snapshot = await CreatorDirectionResolver().snapshot(db, user_id)
            suggestion = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.instruction == "I prefer quick cuts",
                    )
                )
            ).scalar_one()
            assert snapshot.items == ()
            assert suggestion.state == "suggested"
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_revision_fence_and_idempotency_fingerprint_are_enforced():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            await service.set_enabled(db, user_id, False, 0, "toggle-once")

            with pytest.raises(StaleRevision, match="revision is stale"):
                await service.set_enabled(db, user_id, True, 0, "stale-toggle")

            replay = await service.set_enabled(db, user_id, False, 0, "toggle-once")
            assert replay.operation_kind == "set_enabled"
            assert replay.resulting_revision == 1

            with pytest.raises(IdempotencyMismatch, match="different input"):
                await service.set_enabled(db, user_id, True, 1, "toggle-once")
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_undo_is_expiring_and_single_use():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            expired = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="expired-create",
            )
            expired.undo_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.flush()
            with pytest.raises(DirectionConflict, match="cannot be undone"):
                await service.undo(db, user_id, expired.id, 1, "expired-undo")

            active = await service.create_item(
                db,
                user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=1,
                idempotency_key="active-create",
            )
            first_undo = await service.undo(db, user_id, active.id, 2, "active-undo")
            assert first_undo.resulting_revision == 3
            with pytest.raises(DirectionConflict, match="cannot be undone"):
                await service.undo(db, user_id, active.id, 3, "second-undo")

            item = await db.get(CreatorMemoryItem, active.item_id)
            assert item is not None
            assert item.state == "forgotten"
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_clear_preferences_is_owner_scoped_and_undo_restores_all_states():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
        other_user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            active = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows",
                category="avoid",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="clear-active",
            )
            suggested = await service.create_item(
                db,
                user_id,
                instruction="I prefer calm pacing",
                category="stories_pacing",
                enforcement="advisory",
                normalized_key=None,
                structured_value=None,
                expected_revision=1,
                idempotency_key="clear-suggested",
                initial_state="suggested",
            )
            other = await service.create_item(
                db,
                other_user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="other-user-item",
            )
            cleared = await service.clear_preferences(
                db, user_id, expected_revision=2, idempotency_key="clear-all"
            )
            assert cleared.undo_expires_at is not None
            assert (await db.get(CreatorMemoryItem, active.item_id)).state == "forgotten"
            assert (await db.get(CreatorMemoryItem, suggested.item_id)).state == "dismissed"
            assert (await db.get(CreatorMemoryItem, other.item_id)).state == "active"

            undone = await service.undo(
                db,
                user_id,
                cleared.id,
                expected_revision=3,
                idempotency_key="clear-all-undo",
            )
            assert undone.resulting_revision == 4
            assert (await db.get(CreatorMemoryItem, active.item_id)).state == "active"
            assert (await db.get(CreatorMemoryItem, suggested.item_id)).state == "suggested"
            assert (await db.get(CreatorMemoryItem, other.item_id)).state == "active"

            with pytest.raises(DirectionConflict, match="cannot be undone"):
                await service.undo(
                    db,
                    user_id,
                    cleared.id,
                    expected_revision=4,
                    idempotency_key="clear-all-undo-again",
                )
            await db.commit()
    finally:
        await _delete_users(user_id, other_user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_suggestions_require_explicit_accept_or_dismiss():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            accepted = await service.create_item(
                db,
                user_id,
                instruction="I prefer quiet openings",
                category="stories_pacing",
                enforcement="advisory",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="suggest-accept",
                initial_state="suggested",
            )
            await service.set_item_state(
                db,
                user_id,
                accepted.item_id,
                state="active",
                expected_revision=1,
                idempotency_key="accept",
            )
            dismissed = await service.create_item(
                db,
                user_id,
                instruction="I like very fast endings",
                category="stories_pacing",
                enforcement="advisory",
                normalized_key=None,
                structured_value=None,
                expected_revision=2,
                idempotency_key="suggest-dismiss",
                initial_state="suggested",
            )
            await service.set_item_state(
                db,
                user_id,
                dismissed.item_id,
                state="dismissed",
                expected_revision=3,
                idempotency_key="dismiss",
            )
            await db.commit()

            accepted_item = await db.get(CreatorMemoryItem, accepted.item_id)
            dismissed_item = await db.get(CreatorMemoryItem, dismissed.item_id)
            assert accepted_item is not None and accepted_item.state == "active"
            assert accepted_item.user_locked is True
            assert dismissed_item is not None and dismissed_item.state == "dismissed"
            snapshot = await CreatorDirectionResolver().snapshot(db, user_id)
            assert [row["instruction"] for row in snapshot.items] == ["I prefer quiet openings"]
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_conflicting_typed_rule_activates_only_after_accept_and_undo_restores_prior():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            current = await service.create_item(
                db,
                user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="font-current",
            )
            proposed = await service.create_item(
                db,
                user_id,
                instruction="Always use Playfair Display font",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=1,
                idempotency_key="font-proposed",
            )
            proposed_item = await db.get(CreatorMemoryItem, proposed.item_id)
            assert proposed_item is not None and proposed_item.state == "suggested"

            accepted = await service.set_item_state(
                db,
                user_id,
                proposed.item_id,
                state="active",
                expected_revision=2,
                idempotency_key="font-accept",
            )
            current_item = await db.get(CreatorMemoryItem, current.item_id)
            assert current_item is not None and current_item.state == "superseded"
            assert proposed_item.state == "active"

            await service.undo(
                db,
                user_id,
                accepted.id,
                expected_revision=3,
                idempotency_key="font-accept-undo",
            )
            assert current_item.state == "active"
            assert proposed_item.state == "suggested"
            assert (await CreatorDirectionResolver().snapshot(db, user_id)).typed_overrides == {
                "font_family": "Inter"
            }
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_edit_rejects_a_typed_key_owned_by_another_active_item():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            await service.create_item(
                db,
                user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="edit-conflict-font",
            )
            shadow = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows to my videos",
                category="avoid",
                enforcement="constraint",
                normalized_key=None,
                structured_value=None,
                expected_revision=1,
                idempotency_key="edit-conflict-shadow",
            )
            await db.commit()

            with pytest.raises(
                DirectionConflict, match="another active memory item already uses this key"
            ):
                await service.update_item(
                    db,
                    user_id,
                    shadow.item_id,
                    instruction="Always use Playfair Display font",
                    category="video_style",
                    enforcement="constraint",
                    normalized_key=None,
                    structured_value=None,
                    expected_revision=2,
                    idempotency_key="edit-conflict-update",
                )

            await db.rollback()
            snapshot = await CreatorDirectionResolver().snapshot(db, user_id)
            assert snapshot.typed_overrides == {
                "font_family": "Inter",
                "shadow_enabled": False,
            }
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_project_a_rules_shape_b_snapshot_but_edits_only_shape_project_c() -> None:
    service = CreatorDirectionService()
    resolver = CreatorDirectionResolver()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            project_a = CreationThread(creator_id=user_id, title="Project A")
            project_b = CreationThread(creator_id=user_id, title="Project B")
            project_c = CreationThread(creator_id=user_id, title="Project C")
            db.add_all([project_a, project_b, project_c])
            await db.flush()

            font = await service.create_item(
                db,
                user_id,
                instruction="Always use Playfair Display font",
                category="video_style",
                enforcement="constraint",
                normalized_key="font_family",
                structured_value={"font_family": "Playfair Display"},
                expected_revision=0,
                idempotency_key="project-a-font",
                source_kind="creation_thread",
                source_thread_id=project_a.id,
                user_locked=False,
            )
            shadow = await service.create_item(
                db,
                user_id,
                instruction="Never add shadows",
                category="avoid",
                enforcement="constraint",
                normalized_key="shadow_enabled",
                structured_value={"shadow_enabled": False},
                expected_revision=1,
                idempotency_key="project-a-shadow",
                source_kind="creation_thread",
                source_thread_id=project_a.id,
                user_locked=False,
            )

            project_b_direction = await resolver.snapshot(db, user_id, thread_id=project_b.id)
            project_b_state = attach_snapshot(
                {}, project_b_direction, source="project-b-generation"
            )
            rendered_b = apply_direction_overrides(
                [{"text": "Opening", "font_family": "Inter", "shadow_enabled": True}],
                typed_overrides=typed_overrides_from_container(project_b_state),
            )
            assert rendered_b == [
                {
                    "text": "Opening",
                    "font_family": "Playfair Display",
                    "font_cycling": False,
                    "cycle_fonts": [],
                    "shadow_enabled": False,
                }
            ]

            await service.update_item(
                db,
                user_id,
                font.item_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key="font_family",
                structured_value={"font_family": "Inter"},
                expected_revision=2,
                idempotency_key="project-c-font-edit",
            )
            await service.forget(
                db,
                user_id,
                shadow.item_id,
                expected_revision=3,
                idempotency_key="project-c-shadow-forget",
            )

            project_c_direction = await resolver.snapshot(db, user_id, thread_id=project_c.id)
            assert project_c_direction.typed_overrides == {"font_family": "Inter"}
            assert typed_overrides_from_container(project_b_state) == {
                "font_family": "Playfair Display",
                "shadow_enabled": False,
            }
            await db.commit()
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_active_memory_limit_rejects_an_extra_rule(monkeypatch):
    from app.services import creator_direction

    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        monkeypatch.setattr(creator_direction, "MAX_ACTIVE_ITEMS", 1)
        async with AsyncSessionLocal() as db:
            await service.create_item(
                db,
                user_id,
                instruction="Keep the pacing calm",
                category="stories_pacing",
                enforcement="default",
                normalized_key=None,
                structured_value=None,
                expected_revision=0,
                idempotency_key="limit-first",
            )
            with pytest.raises(LimitReached, match="too many active"):
                await service.create_item(
                    db,
                    user_id,
                    instruction="Use quiet music",
                    category="video_style",
                    enforcement="default",
                    normalized_key=None,
                    structured_value=None,
                    expected_revision=1,
                    idempotency_key="limit-second",
                )
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_project_override_is_owner_scoped_and_upserts_one_key():
    service = CreatorDirectionService()
    try:
        owner_id = await _create_user()
        stranger_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            thread = CreationThread(creator_id=owner_id)
            db.add(thread)
            await db.flush()

            created = await service.set_override(
                db,
                owner_id,
                thread.id,
                instruction="Use Inter for this project",
                normalized_key="font_family",
                structured_value={"font_family": "Inter"},
                expected_revision=0,
                idempotency_key="override-create",
            )
            replay = await service.set_override(
                db,
                owner_id,
                thread.id,
                instruction="Use Inter for this project",
                normalized_key="font_family",
                structured_value={"font_family": "Inter"},
                expected_revision=0,
                idempotency_key="override-create",
            )
            assert replay == created
            updated = await service.set_override(
                db,
                owner_id,
                thread.id,
                instruction="Use Playfair for this project",
                normalized_key="font_family",
                structured_value={"font_family": "Playfair Display"},
                expected_revision=1,
                idempotency_key="override-update",
            )
            assert updated.id == created.id

            with pytest.raises(StaleRevision, match="revision is stale"):
                await service.set_override(
                    db,
                    owner_id,
                    thread.id,
                    instruction="Use Montserrat for this project",
                    normalized_key="font_family",
                    structured_value={"font_family": "Montserrat"},
                    expected_revision=1,
                    idempotency_key="override-stale",
                )

            with pytest.raises(DirectionError, match="creation thread not found"):
                await service.set_override(
                    db,
                    stranger_id,
                    thread.id,
                    instruction="Use Inter",
                    normalized_key="font_family",
                    structured_value={"font_family": "Inter"},
                    expected_revision=0,
                    idempotency_key="override-stranger",
                )
            await db.commit()

            rows = (
                (
                    await db.execute(
                        select(ProjectDirectionOverride).where(
                            ProjectDirectionOverride.thread_id == thread.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].structured_value == {"font_family": "Playfair Display"}
            snapshot = await CreatorDirectionResolver().snapshot(db, owner_id, thread_id=thread.id)
            assert snapshot.typed_overrides == {"font_family": "Playfair Display"}

            deleted = await service.delete_override(
                db,
                owner_id,
                thread.id,
                "font_family",
                expected_revision=2,
                idempotency_key="override-delete",
            )
            delete_replay = await service.delete_override(
                db,
                owner_id,
                thread.id,
                "font_family",
                expected_revision=2,
                idempotency_key="override-delete",
            )
            assert delete_replay.id == deleted.id
            assert delete_replay.resulting_revision == 3
            await db.commit()
            assert (
                await db.execute(
                    select(ProjectDirectionOverride).where(
                        ProjectDirectionOverride.thread_id == thread.id
                    )
                )
            ).scalar_one_or_none() is None
    finally:
        await _delete_users(owner_id, stranger_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_extractor_supersedes_only_unlocked_memory_and_undo_restores_it():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            created = await service.create_item(
                db,
                user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key="font_family",
                structured_value={"font_family": "Inter"},
                expected_revision=0,
                idempotency_key="extract-old",
                source_kind="creation_thread",
                user_locked=False,
            )
            replaced = await service.apply_extracted_transition(
                db,
                user_id,
                operation_kind="supersede",
                target_item_id=created.item_id,
                instruction="From now on use Playfair Display font",
                category="video_style",
                enforcement="constraint",
                normalized_key="font_family",
                structured_value={"font_family": "Playfair Display"},
                expected_revision=1,
                idempotency_key="extract-replace",
                source_thread_id=None,
                source_event_id=None,
            )
            assert replaced.operation_kind == "extracted_supersede"
            current = await CreatorDirectionResolver().snapshot(db, user_id)
            assert current.typed_overrides == {"font_family": "Playfair Display"}

            await service.undo(
                db,
                user_id,
                replaced.id,
                expected_revision=2,
                idempotency_key="extract-undo",
            )
            restored = await CreatorDirectionResolver().snapshot(db, user_id)
            assert restored.typed_overrides == {"font_family": "Inter"}
            await db.commit()
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_extractor_cannot_supersede_a_locked_creator_rule():
    service = CreatorDirectionService()
    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            created = await service.create_item(
                db,
                user_id,
                instruction="Always use Inter font",
                category="video_style",
                enforcement="constraint",
                normalized_key="font_family",
                structured_value={"font_family": "Inter"},
                expected_revision=0,
                idempotency_key="locked-old",
                user_locked=True,
            )
            with pytest.raises(DirectionConflict, match="locked memory"):
                await service.apply_extracted_transition(
                    db,
                    user_id,
                    operation_kind="supersede",
                    target_item_id=created.item_id,
                    instruction="From now on use Playfair Display font",
                    category="video_style",
                    enforcement="constraint",
                    normalized_key="font_family",
                    structured_value={"font_family": "Playfair Display"},
                    expected_revision=1,
                    idempotency_key="locked-replace",
                    source_thread_id=None,
                    source_event_id=None,
                )
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_outbox_failure_retries_then_moves_to_dead_letter(monkeypatch):
    from app.tasks import creator_memory as creator_memory_task

    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", True)
        async with AsyncSessionLocal() as db:
            retry = CreatorMemoryOutbox(
                user_id=user_id,
                source_message="Always use captions",
                status="leased",
                attempts=2,
            )
            dead = CreatorMemoryOutbox(
                user_id=user_id,
                source_message="Never use shadows",
                status="leased",
                attempts=5,
            )
            db.add_all([retry, dead])
            await db.commit()
            retry_id, dead_id = retry.id, dead.id

        monkeypatch.setattr(
            creator_memory_task,
            "_apply_deterministic_direction",
            AsyncMock(side_effect=RuntimeError("provider unavailable")),
        )
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(retry_id))
            == "extraction_failed"
        )
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(dead_id))
            == "extraction_failed"
        )

        async with AsyncSessionLocal() as db:
            retry_row = await db.get(CreatorMemoryOutbox, retry_id)
            dead_row = await db.get(CreatorMemoryOutbox, dead_id)
            assert retry_row is not None and retry_row.status == "pending"
            assert retry_row.result_code == "RuntimeError"
            assert dead_row is not None and dead_row.status == "dead"
            assert dead_row.result_code == "RuntimeError"
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_outbox_handles_disabled_missing_noop_and_success_states(monkeypatch):
    from app.tasks import creator_memory as creator_memory_task

    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            rows = [
                CreatorMemoryOutbox(
                    user_id=user_id,
                    source_message="Always use captions",
                    status="leased",
                    attempts=1,
                ),
                CreatorMemoryOutbox(
                    user_id=user_id, source_message=" ", status="leased", attempts=1
                ),
                CreatorMemoryOutbox(
                    user_id=user_id,
                    source_message="What is the render status?",
                    status="leased",
                    attempts=1,
                ),
                CreatorMemoryOutbox(
                    user_id=user_id,
                    source_message="Never add shadows",
                    status="leased",
                    attempts=1,
                ),
            ]
            db.add_all(rows)
            await db.commit()
            disabled_id, missing_id, noop_id, success_id = [row.id for row in rows]

        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", False)
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(disabled_id))
            == "disabled"
        )
        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", True)
        assert creator_memory_task.process_outbox("not-a-uuid") == "invalid_id"
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(missing_id))
            == "source_event_missing"
        )
        assert await asyncio.to_thread(creator_memory_task.process_outbox, str(noop_id)) == "noop"
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(success_id))
            == "create_item"
        )

        async with AsyncSessionLocal() as db:
            assert (await db.get(CreatorMemoryOutbox, disabled_id)).status == "pending"
            assert (await db.get(CreatorMemoryOutbox, missing_id)).status == "succeeded"
            assert (await db.get(CreatorMemoryOutbox, noop_id)).status == "succeeded"
            assert (await db.get(CreatorMemoryOutbox, success_id)).status == "succeeded"
            learned = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.normalized_key == "shadow_enabled",
                    )
                )
            ).scalar_one()
            assert learned.state == "active"
            assert learned.user_locked is False
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_project_deletion_preserves_pending_outbox_and_nulls_source_event(monkeypatch):
    from app.tasks import creator_memory as creator_memory_task

    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", True)
        async with AsyncSessionLocal() as db:
            thread = CreationThread(creator_id=user_id)
            db.add(thread)
            await db.flush()
            source = CreationThreadEvent(
                thread_id=thread.id,
                sequence=0,
                revision=0,
                role="user",
                event_type="user_message",
                content="Never add shadows",
            )
            db.add(source)
            await db.flush()
            outbox = CreatorMemoryOutbox(
                user_id=user_id,
                source_event_id=source.id,
                source_message="Never add shadows",
                status="pending",
            )
            db.add(outbox)
            await db.commit()
            outbox_id, thread_id = outbox.id, thread.id

        async with AsyncSessionLocal() as db:
            await db.execute(delete(CreationThread).where(CreationThread.id == thread_id))
            await db.commit()

            preserved = await db.get(CreatorMemoryOutbox, outbox_id)
            assert preserved is not None
            assert preserved.status == "pending"
            assert preserved.source_event_id is None
            assert preserved.source_message == "Never add shadows"

        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(outbox_id))
            == "create_item"
        )

        async with AsyncSessionLocal() as db:
            processed = await db.get(CreatorMemoryOutbox, outbox_id)
            assert processed is not None
            assert processed.status == "succeeded"
            learned = (
                await db.execute(
                    select(CreatorMemoryItem).where(
                        CreatorMemoryItem.user_id == user_id,
                        CreatorMemoryItem.instruction == "Never add shadows",
                    )
                )
            ).scalar_one()
            assert learned.source_event_id is None
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_explicit_project_learning_appends_one_undo_receipt(monkeypatch):
    from app.tasks import creator_memory as creator_memory_task

    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", True)
        async with AsyncSessionLocal() as db:
            thread = CreationThread(creator_id=user_id)
            db.add(thread)
            await db.flush()
            source = CreationThreadEvent(
                thread_id=thread.id,
                sequence=0,
                revision=0,
                role="user",
                event_type="user_message",
                content="Never add shadows",
            )
            db.add(source)
            await db.flush()
            outbox = CreatorMemoryOutbox(
                user_id=user_id,
                source_event_id=source.id,
                source_message="Never add shadows",
                status="leased",
                attempts=1,
            )
            db.add(outbox)
            await db.commit()
            outbox_id = outbox.id
            thread_id = thread.id

        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(outbox_id))
            == "create_item"
        )
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(outbox_id)) == "ignored"
        )

        async with AsyncSessionLocal() as db:
            receipts = (
                (
                    await db.execute(
                        select(CreationThreadEvent).where(
                            CreationThreadEvent.thread_id == thread_id,
                            CreationThreadEvent.event_type == "memory_updated",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(receipts) == 1
            assert receipts[0].content == "Remembered for future videos."
            assert receipts[0].payload["memory_revision"] == 1
            assert receipts[0].payload["operation_id"]
            assert receipts[0].payload["undo_expires_at"]
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_creation_thread_receipt_projects_durable_undone_operation_state():
    from app.routes.creation_threads import _response

    try:
        user_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        async with AsyncSessionLocal() as db:
            thread = CreationThread(creator_id=user_id)
            db.add(thread)
            await db.flush()
            operation = CreatorMemoryOperation(
                user_id=user_id,
                idempotency_key="receipt-undone",
                request_fingerprint="fingerprint",
                operation_kind="create_item",
                resulting_revision=1,
                actor_kind="system",
                undone_at=datetime.now(UTC),
            )
            db.add(operation)
            await db.flush()
            db.add(
                CreationThreadEvent(
                    thread_id=thread.id,
                    sequence=0,
                    revision=0,
                    role="assistant",
                    event_type="memory_updated",
                    content="Remembered for future videos.",
                    payload={
                        "kind": "creator_memory_receipt",
                        "operation_id": str(operation.id),
                        "memory_revision": 1,
                        "undo_expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    },
                )
            )
            await db.commit()
            await db.refresh(thread)

            response = await _response(db, thread)
            assert response.events[0].payload["undone"] is True
            assert response.events[0].payload["undone_at"]
    finally:
        await _delete_users(user_id)


@pytest.mark.asyncio(loop_scope="module")
async def test_outbox_rejects_unknown_payload_and_cross_owner_source(monkeypatch):
    from app.tasks import creator_memory as creator_memory_task

    try:
        owner_id = await _create_user()
        other_id = await _create_user()
    except (OperationalError, OSError) as exc:
        pytest.skip(f"nova_test Postgres not reachable: {exc!r}")
    try:
        monkeypatch.setattr(creator_memory_task.settings, "creator_memory_enabled", True)
        async with AsyncSessionLocal() as db:
            other_thread = CreationThread(creator_id=other_id)
            db.add(other_thread)
            await db.flush()
            other_event = CreationThreadEvent(
                thread_id=other_thread.id,
                sequence=0,
                revision=0,
                role="user",
                event_type="user_message",
                content="Always use captions",
            )
            db.add(other_event)
            await db.flush()
            unknown = CreatorMemoryOutbox(
                user_id=owner_id,
                source_message="Always use captions",
                payload_version=999,
                status="leased",
                attempts=1,
            )
            wrong_owner = CreatorMemoryOutbox(
                user_id=owner_id,
                source_event_id=other_event.id,
                source_message="Always use captions",
                payload_version=1,
                status="leased",
                attempts=1,
            )
            db.add_all([unknown, wrong_owner])
            await db.commit()
            unknown_id, wrong_owner_id = unknown.id, wrong_owner.id

        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(unknown_id))
            == "unsupported_payload_version"
        )
        assert (
            await asyncio.to_thread(creator_memory_task.process_outbox, str(wrong_owner_id))
            == "source_owner_mismatch"
        )
        async with AsyncSessionLocal() as db:
            unknown_row = await db.get(CreatorMemoryOutbox, unknown_id)
            wrong_owner_row = await db.get(CreatorMemoryOutbox, wrong_owner_id)
            assert unknown_row is not None and unknown_row.status == "dead"
            assert wrong_owner_row is not None and wrong_owner_row.status == "dead"
    finally:
        await _delete_users(owner_id, other_id)
