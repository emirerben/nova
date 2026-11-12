"""Real-Postgres coverage for the invariant "the editor draft head is on the current generation".

Each test is one failure mode from
``docs/reviews/latest-wins-convergence/server-failure-modes.md``. Assertions are rows and
returned documents only: how many head rows exist, which generation they name, what the
document says.
"""

from __future__ import annotations

import asyncio
import copy
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.editor_head import (
    current_bases,
    is_head_current,
    restage_editor_head,
    restage_editor_head_sync,
)
from app.models import CreatorEditDraft, Job, PlanItem
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.editor_empty_drafts import attach_saved_editor_drafts
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

VARIANT = "original_text"
HOOK = {
    "id": "hook",
    "text": "Old matcha hook",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "font_family": "Playfair Display",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _variant(
    generation: str | None = "generation-1", text: str = "Old matcha hook", **extra
) -> dict:  # noqa: ANN003
    variant = {
        "variant_id": VARIANT,
        "resolved_archetype": "montage",
        "render_status": "ready",
        "render_finished_at": "2026-09-07T08:00:00Z",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "text_elements": [{**HOOK, "text": text}],
        **extra,
    }
    if generation is not None:
        variant["render_generation_id"] = generation
    return variant


def _add_job(item_id, user_id, variant: dict, *, current: bool = True, **plan) -> uuid.UUID:  # noqa: ANN001, ANN003
    job_id = uuid.uuid4()
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item_id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
                assembly_plan={"variants": [variant], **plan},
            )
        )
        db.flush()
        if current:
            item.current_job_id = job_id
        db.commit()
    return job_id


def _seed(variant: dict | None = None):  # noqa: ANN202
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        from app.models import CreationThread

        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    job_id = _add_job(item_id, user_id, variant or _variant())
    return user_id, thread_id, item_id, job_id


def _update_variant(job_id, **changes) -> None:  # noqa: ANN001, ANN003
    """Move the variant the way a render/dispatcher does: replace the JSON dict, never mutate."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        assembly = copy.deepcopy(job.assembly_plan)
        variant = assembly["variants"][0]
        for key, value in changes.items():
            if value is None:
                variant.pop(key, None)
            else:
                variant[key] = value
        job.assembly_plan = assembly
        db.commit()


def _publish_from(job_id, *, source: str, attempt: str) -> None:  # noqa: ANN001
    """A phone publish: the variant now carries the attempt id; the record names the source."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        assembly = copy.deepcopy(job.assembly_plan)
        assembly["variants"][0]["render_generation_id"] = attempt
        assembly[DEVICE_RENDER_FIELD] = {
            VARIANT: {
                "published_attempt": attempt,
                "status": {"phase": "published", "base_generation": source},
            }
        }
        job.assembly_plan = assembly
        db.commit()


async def _stage_ai_edit(user_id, thread_id, text: str = "AI hook"):  # noqa: ANN001, ANN202
    """The AI chat edit: a draft head with new text, staged on the variant's generation."""
    async with AsyncSessionLocal() as db:
        initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    payload = copy.deepcopy(initial.snapshot["editor_payload"])
    payload["sections"]["text_elements"][0]["text"] = text
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=initial.draft_revision,
            expected_etag=initial.etag,
            snapshot={
                **initial.snapshot,
                "editor_payload": payload,
                "changes": ["Retitle the hook"],
                "editor_text_diff": [{"index": 0, "before": "Old matcha hook", "after": text}],
            },
        )


async def _stage_ai_edit_over(user_id, thread_id, text: str):  # noqa: ANN001, ANN202
    """A second chat edit on top of the current head."""
    async with AsyncSessionLocal() as db:
        head = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    payload = copy.deepcopy(head.snapshot["editor_payload"])
    payload["sections"]["text_elements"][0]["text"] = text
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=head.draft_revision,
            expected_etag=head.etag,
            snapshot={**head.snapshot, "editor_payload": payload, "changes": ["Second edit"]},
        )


def _restage(  # noqa: ANN202
    item_id,  # noqa: ANN001
    job_id,  # noqa: ANN001
    user_id,  # noqa: ANN001
    *,
    contained: bool,
    reason: str = "test",
    lock_parents: bool = True,
):
    """Run it as the callers do: Item then Job locked first, head locked last by the module."""
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=lock_parents)
        job = db.get(Job, job_id, with_for_update=lock_parents)
        row = restage_editor_head_sync(
            db,
            item=item,
            job=job,
            variant_key=VARIANT,
            creator_id=user_id,
            committed_contains_head=contained,
            reason=reason,
        )
        row_id = row.id if row is not None else None
        db.commit()
        return row_id


def _rows(item_id) -> list[dict]:  # noqa: ANN001
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.variant_key == VARIANT)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "rev": row.draft_revision,
                "parent": row.parent_draft_id,
                "head": row.is_head,
                "job": row.base_job_id,
                "col": row.base_generation_id,
                "payload_base": ((row.snapshot_json or {}).get("editor_payload") or {}).get(
                    "base_generation"
                ),
                "snap": row.snapshot_json,
            }
            for row in rows
        ]


def _head(item_id) -> dict:  # noqa: ANN001
    heads = [row for row in _rows(item_id) if row["head"]]
    assert len(heads) == 1, "exactly one head row per (item, variant)"
    return heads[0]


def _text(row: dict) -> str:
    return row["snap"]["editor_payload"]["sections"]["text_elements"][0]["text"]


def _current(item_id, job_id) -> bool:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, job_id)
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == item_id,
                CreatorEditDraft.variant_key == VARIANT,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        return is_head_current(job, job.assembly_plan["variants"][0], head)


@pytest.mark.asyncio
async def test_approval_advance_restages_head_as_committed_state_with_empty_diff() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        assert _current(item_id, job_id)
        # The approved edit rendered: the variant moves to G2 and already shows the AI text.
        _update_variant(
            job_id, render_generation_id="generation-2", text_elements=[{**HOOK, "text": "AI hook"}]
        )
        assert not _current(item_id, job_id)

        new_id = _restage(item_id, job_id, user_id, contained=True, reason="approval")

        head = _head(item_id)
        assert head["id"] == new_id
        assert head["parent"] == uuid.UUID(staged.draft_id)
        assert head["rev"] == staged.draft_revision + 1
        assert head["job"] == job_id
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _text(head) == "AI hook"
        assert head["snap"]["changes"] == []
        assert "editor_text_diff" not in head["snap"]
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_phone_publish_head_is_current_by_source_then_restaged_to_the_exact_id() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant("generation-2"))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _publish_from(job_id, source="generation-2", attempt="attempt-3")

        with sync_session() as db:
            job = db.get(Job, job_id)
            variant = job.assembly_plan["variants"][0]
            assert current_bases(job, variant) == frozenset({"attempt-3", "generation-2"})
        # The head still names the pre-publish generation; the publish record vouches for it.
        assert _head(item_id)["col"] == "generation-2"
        assert _current(item_id, job_id)

        _restage(item_id, job_id, user_id, contained=False, reason="publish")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "attempt-3"
        assert _text(head) == "AI hook", "the creator's staged edit survives the id swap"
        assert head["snap"]["changes"] == ["Retitle the hook"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_legacy_dispatcher_bump_strands_the_head_until_restaged() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        # A dispatcher re-renders from other state: new generation, other committed music.
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )
        assert not _current(item_id, job_id)

        _restage(item_id, job_id, user_id, contained=False, reason="dispatch")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-legacy"
        assert _text(head) == "AI hook", "a lane only the head changed rides on the new generation"
        assert head["snap"]["editor_payload"]["sections"]["music_track_id"] == "song-dispatch"
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_retried_restage_inserts_zero_additional_rows() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")

        first = _restage(item_id, job_id, user_id, contained=True, reason="approval")
        rows_after_first = _rows(item_id)
        second = _restage(item_id, job_id, user_id, contained=True, reason="approval")
        third = _restage(item_id, job_id, user_id, contained=False, reason="approval")

        assert first is not None
        assert second is None and third is None
        assert _rows(item_id) == rows_after_first
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_head_from_an_older_job_is_not_current_and_is_rebased_onto_the_current_job() -> None:
    user_id, thread_id, item_id, old_job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "Old job AI hook")
        # A regenerate: the item now points at a new job with its own render lineage.
        new_job_id = _add_job(item_id, user_id, _variant("generation-1", text="New job hook"))
        assert _head(item_id)["job"] == old_job_id
        # Same generation string on purpose: only the job differs.
        assert not _current(item_id, new_job_id)

        _restage(item_id, new_job_id, user_id, contained=False, reason="regenerate")

        head = _head(item_id)
        assert head["job"] == new_job_id
        assert head["col"] == head["payload_base"] == "generation-1"
        assert _text(head) == "New job hook", "old-job lanes are never replayed on another timeline"
        assert _current(item_id, new_job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_pinned_editor_draft_on_a_v2_variant_follows_the_restaged_head() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(
            job_id,
            editor_state_version=2,
            editor_draft={"draft_id": staged.draft_id},
        )

        async def pinned_draft_ids() -> dict:
            async with AsyncSessionLocal() as db:
                job = await db.get(Job, job_id)
                await attach_saved_editor_drafts(db, job)
                return {k: v.draft_id for k, v in job._saved_editor_drafts.items()}

        assert await pinned_draft_ids() == {VARIANT: staged.draft_id}
        _update_variant(job_id, render_generation_id="generation-2")
        assert await pinned_draft_ids() == {}, "timeline GET / Save would 409 here"

        new_id = _restage(item_id, job_id, user_id, contained=True, reason="approval")

        with sync_session() as db:
            variant = db.get(Job, job_id).assembly_plan["variants"][0]
            assert variant["editor_draft"] == {"draft_id": str(new_id)}
            assert variant["editor_state_version"] == 2
        assert await pinned_draft_ids() == {VARIANT: str(new_id)}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_after_a_restage_leaves_a_head_that_is_exactly_current() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")
        _restage(item_id, job_id, user_id, contained=False, reason="observe")
        restaged = _head(item_id)

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=restaged["rev"]
            )

        head = _head(item_id)
        assert head["id"] == uuid.UUID(undone.draft_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _current(item_id, job_id)
        assert undone.head_current is True and undone.base_generation == "generation-2"
        assert _text(head) == "Old matcha hook", (
            "undo skips the restage row and restores the parent's lanes on the current generation"
        )
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_of_a_head_on_an_old_generation_still_lands_current() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        second = await _stage_ai_edit_over(user_id, thread_id, "Second hook")
        _update_variant(job_id, render_generation_id="generation-2")

        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=second.draft_revision
            )

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _current(item_id, job_id)
        assert undone.head_current is True
        assert _text(head) == "AI hook"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_is_current_only_when_column_and_payload_bases_both_are() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id)
        assert _current(item_id, job_id)

        def set_bases(column: str, payload: str) -> None:
            with sync_session() as db:
                row = db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
                    )
                ).scalar_one()
                snapshot = copy.deepcopy(row.snapshot_json)
                snapshot["editor_payload"]["base_generation"] = payload
                row.snapshot_json = snapshot
                row.base_generation_id = column
                db.commit()

        set_bases("generation-1", "generation-0")
        assert not _current(item_id, job_id)
        set_bases("generation-0", "generation-1")
        assert not _current(item_id, job_id)
        set_bases("generation-1", "generation-1")
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_variant_with_only_render_finished_at_is_stamped_and_restaged_on_it() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(None))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "2026-09-07T08:00:00Z"
        assert _current(item_id, job_id)

        _update_variant(job_id, render_finished_at="2026-09-07T09:00:00Z")
        assert not _current(item_id, job_id)
        _restage(item_id, job_id, user_id, contained=False, reason="baseline")

        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "2026-09-07T09:00:00Z"
        assert _text(head) == "AI hook"
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_variant_that_never_finished_a_render_has_a_current_head_and_no_heal_rows() -> None:
    variant = _variant(None)
    variant.pop("render_finished_at")
    user_id, thread_id, item_id, job_id = _seed(variant)
    try:
        async with AsyncSessionLocal() as db:
            first = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        async with AsyncSessionLocal() as db:
            again = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert first.draft_id == again.draft_id
        assert again.head_current is True and again.base_generation == ""
        assert _current(item_id, job_id)
        assert len(_rows(item_id)) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_non_editor_head_and_a_missing_head_are_left_alone() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    try:
        with sync_session() as db:
            from app.models import CreationThread

            item_id = db.get(CreationThread, thread_id).active_plan_item_id
        # No job yet: the bootstrap draft is an "initial" body with no editor payload.
        async with AsyncSessionLocal() as db:
            initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert initial.snapshot["kind"] == "initial"
        job_id = _add_job(item_id, user_id, _variant("generation-2"))
        before = _rows(item_id)

        assert _restage(item_id, job_id, user_id, contained=True) is None
        assert _rows(item_id) == before

        other_user, _other_thread, other_item, other_job = _seed()
        assert _restage(other_item, other_job, other_user, contained=True) is None
        assert _rows(other_item) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_restage_waits_for_a_concurrent_writer_and_rebases_the_writers_revision() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")
        holding, release = threading.Event(), threading.Event()

        def writer() -> None:
            with sync_session() as db:
                head = db.execute(
                    select(CreatorEditDraft)
                    .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True))
                    .with_for_update()
                ).scalar_one()
                holding.set()
                assert release.wait(20)
                snapshot = copy.deepcopy(head.snapshot_json)
                snapshot["editor_payload"]["sections"]["text_elements"][0]["text"] = "Writer text"
                head.is_head = False
                db.add(
                    CreatorEditDraft(
                        creator_id=head.creator_id,
                        thread_id=head.thread_id,
                        item_id=head.item_id,
                        variant_key=head.variant_key,
                        base_job_id=head.base_job_id,
                        base_generation_id=head.base_generation_id,
                        draft_revision=head.draft_revision + 1,
                        parent_draft_id=head.id,
                        snapshot_json=snapshot,
                        snapshot_hash="writer-" + uuid.uuid4().hex,
                        is_head=True,
                    )
                )
                db.commit()

        writer_task = asyncio.create_task(asyncio.to_thread(writer))
        await asyncio.to_thread(holding.wait, 20)
        restage_task = asyncio.create_task(
            asyncio.to_thread(
                _restage,
                item_id,
                job_id,
                user_id,
                contained=False,
                reason="race",
                # The writer below locks only the head, so it would deadlock against a held
                # Item/Job lock through its foreign keys. Real writers take Item first.
                lock_parents=False,
            )
        )
        await asyncio.sleep(0.6)
        assert not restage_task.done(), "the restage must wait on the head lock the writer holds"
        release.set()
        await writer_task
        assert await restage_task is not None

        rows = _rows(item_id)
        head = _head(item_id)
        assert [r["rev"] for r in rows] == [0, 1, 2, 3]
        assert head["rev"] == 3 and head["parent"] == rows[2]["id"]
        assert _text(head) == "Writer text", "the revision that won the lock is what gets rebased"
        assert head["col"] == head["payload_base"] == "generation-2"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_async_twin_restages_exactly_like_the_sync_function() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")

        async def run() -> uuid.UUID | None:
            async with AsyncSessionLocal() as db:
                item = (
                    await db.execute(
                        select(PlanItem).where(PlanItem.id == item_id).with_for_update()
                    )
                ).scalar_one()
                job = (
                    await db.execute(select(Job).where(Job.id == job_id).with_for_update())
                ).scalar_one()
                row = await restage_editor_head(
                    db,
                    item=item,
                    job=job,
                    variant_key=VARIANT,
                    creator_id=user_id,
                    committed_contains_head=True,
                    reason="approval",
                )
                row_id = row.id if row is not None else None
                await db.commit()
                return row_id

        first = await run()
        head = _head(item_id)
        assert first == head["id"]
        assert head["col"] == head["payload_base"] == "generation-2"
        assert await run() is None
        assert len(_rows(item_id)) == 3
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_reading_a_stranded_head_serves_a_current_head_with_the_ai_lanes_intact() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        assert staged.head_current is True
        _update_variant(
            job_id, render_generation_id="generation-legacy", music_track_id="song-dispatch"
        )
        assert not _current(item_id, job_id)

        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert served.head_current is True
        assert served.base_generation == "generation-legacy"
        assert served.base_generation_id == "generation-legacy"
        assert served.snapshot["editor_payload"]["base_generation"] == "generation-legacy"
        assert served.snapshot["editor_payload"]["sections"]["text_elements"][0]["text"] == (
            "AI hook"
        ), "the same-job head is the creator's newest intent and stays reachable"
        assert served.snapshot["changes"] == ["Retitle the hook"]
        assert _current(item_id, job_id)

        rows_after_heal = _rows(item_id)
        async with AsyncSessionLocal() as db:
            again = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert again.draft_id == served.draft_id
        assert _rows(item_id) == rows_after_heal, "a healed head is read, not restaged again"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_reading_a_head_published_from_its_generation_is_moved_to_the_exact_id() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant("generation-2"))
    try:
        await _stage_ai_edit(user_id, thread_id, "AI hook")
        _publish_from(job_id, source="generation-2", attempt="attempt-3")

        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)

        assert served.base_generation == "attempt-3" and served.head_current is True
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "attempt-3"
        assert _text(head) == "AI hook"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_writing_onto_a_stranded_head_succeeds_and_lands_current() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")

        payload = copy.deepcopy(staged.snapshot["editor_payload"])
        payload["sections"]["text_elements"][0]["text"] = "Creator hook"
        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=staged.draft_revision,
                expected_etag=staged.etag,
                snapshot={**staged.snapshot, "editor_payload": payload, "changes": ["Creator"]},
            )

        head = _head(item_id)
        assert head["id"] == uuid.UUID(written.draft_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _text(head) == "Creator hook"
        assert written.head_current is True and written.base_generation == "generation-2"
        assert _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_write_with_a_stale_revision_is_still_refused_as_draft_stale() -> None:
    from app.kria.runtime import RuntimeFailure

    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        _update_variant(job_id, render_generation_id="generation-2")
        async with AsyncSessionLocal() as db:
            await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        rows_before = _rows(item_id)

        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as raised:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    expected_revision=staged.draft_revision - 1,
                    expected_etag=staged.etag,
                    snapshot=staged.snapshot,
                )
        assert raised.value.code == "draft_stale"
        assert _rows(item_id) == rows_before
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_locking_the_job_rereads_a_generation_committed_after_the_target_was_loaded() -> None:
    from app.kria.drafts import _lock_job, _target

    user_id, thread_id, item_id, job_id = _seed()
    try:
        async with AsyncSessionLocal() as db:
            target = await _target(db, thread_id=thread_id, creator_id=user_id, lock_item=True)
            assert target.generation_id == "generation-1"
            _update_variant(job_id, render_generation_id="generation-2")
            relocked = await _lock_job(db, target)
            assert relocked.generation_id == "generation-2"
    finally:
        await async_engine.dispose()


def test_current_bases_drops_empties_and_falls_back_to_render_finished_at() -> None:
    job = SimpleNamespace(assembly_plan={})
    assert current_bases(job, {"variant_id": VARIANT}) == frozenset()
    assert current_bases(job, {"variant_id": VARIANT, "render_finished_at": "t1"}) == frozenset(
        {"t1"}
    )
    assert current_bases(
        job, {"variant_id": VARIANT, "render_generation_id": "g1", "render_finished_at": "t1"}
    ) == frozenset({"g1"})
